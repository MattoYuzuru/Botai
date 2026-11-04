import { type ReactNode, useId } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

export type StatTrendDirection = "up" | "down" | "flat";

export interface StatTrend {
  direction: StatTrendDirection;
  /** Что изменилось, словами источника: «+0,4 к прошлому семестру». Без текста тренда нет. */
  label: string;
}

const TREND_ICON = { up: "trend", down: "trend-down", flat: "minus" } as const satisfies Record<
  StatTrendDirection,
  IconName
>;
const TREND_WORD = { up: "рост", down: "снижение", flat: "без изменений" } as const;

export interface StatTileProps {
  /** Что измерено: «Средний балл», «Место в рейтинге». */
  label: string;
  /** Значение, как его дал источник: «8,4», «12», «зачтено». Ничего не пересчитывается. */
  value: string | number;
  /** Шкала источника: «10» рисует «8,4 / 10». Показывается только если источник её назвал. */
  scale?: string | number | null;
  /** Единица после значения: «баллов», «кредитов». */
  unit?: string | null;
  /** Пояснение под значением: «по 7 предметам», «обновлено 3 ноября». */
  caption?: ReactNode;
  /** Изменение. Нейтрально по цвету: хорошо это или плохо, знает только источник; направление — иконка и слово. */
  trend?: StatTrend | null;
  icon?: IconName;
  /** Плотный вариант для узких мест: меньше отступы и кегль значения на любой ширине. */
  compact?: boolean;
  className?: string;
}

/**
 * Плитка показателя (KPI): подпись, крупное значение, необязательные шкала, единица, подпись и тренд. На телефоне
 * плитка уже и значение мельче (`type-title`), от 768 px — крупнее; текст переносится, а не обрезается, поэтому
 * плитка не расширяет колонку. Собирается в `StatGrid`. Плитка не оценивает значение и не считает проценты.
 */
export function StatTile({
  label,
  value,
  scale,
  unit,
  caption,
  trend,
  icon,
  compact = false,
  className,
}: StatTileProps) {
  const labelId = useId();
  const hasScale = scale !== undefined && scale !== null && `${scale}` !== "";
  return (
    // biome-ignore lint/a11y/useSemanticElements: fieldset — для полей формы; плитка — группа «подпись + значение»
    <div
      role="group"
      aria-labelledby={labelId}
      data-stat-tile=""
      data-compact={compact ? "" : undefined}
      className={cn(
        "flex min-w-0 flex-col gap-1 rounded-lg border border-border bg-surface",
        compact ? "p-3" : "p-3 md:p-4",
        className,
      )}
    >
      <p className="flex items-start gap-1.5 type-label text-fg-muted">
        {icon && <Icon name={icon} size={16} className="mt-px" />}
        <span id={labelId} className="min-w-0 break-words">
          {label}
        </span>
      </p>
      <p
        data-stat-value=""
        className={cn(
          "flex flex-wrap items-baseline gap-x-1.5 break-words text-fg",
          compact ? "type-title" : "type-title md:type-display",
        )}
      >
        <span data-numeric="">{value}</span>
        {hasScale && (
          <span className="type-ui text-fg-muted" data-numeric="">
            <span aria-hidden="true">/ </span>
            <span className="sr-only">из </span>
            {scale}
          </span>
        )}
        {unit && <span className="type-ui text-fg-muted">{unit}</span>}
      </p>
      {trend && (
        <p className="flex items-start gap-1 type-caption text-fg-muted" data-trend={trend.direction}>
          <Icon name={TREND_ICON[trend.direction]} size={16} className="mt-px" />
          <span className="min-w-0 break-words">
            <span className="sr-only">{TREND_WORD[trend.direction]}: </span>
            {trend.label}
          </span>
        </p>
      )}
      {caption && <p className="type-caption text-fg-muted">{caption}</p>}
    </div>
  );
}

export interface StatGridProps {
  children: ReactNode;
  /**
   * Наибольшее число колонок на широком экране. Телефон — всегда две (плитки в ряд с переносом); нечётная
   * последняя плитка на телефоне занимает всю строку.
   */
  columns?: 2 | 3 | 4;
  /** Подпись группы показателей для скринридера. */
  label?: string;
  className?: string;
}

const COLUMNS = {
  2: "md:grid-cols-2",
  3: "md:grid-cols-3",
  4: "md:grid-cols-3 lg:grid-cols-4",
} as const;

/** Сетка плиток: две колонки на телефоне, до `columns` на ноутбуке; плитка не шире своей колонки (`min-w-0`). */
export function StatGrid({ children, columns = 3, label, className }: StatGridProps) {
  return (
    <div
      {...(label ? { role: "group", "aria-label": label } : {})}
      data-stat-grid=""
      className={cn(
        "grid min-w-0 grid-cols-2 gap-2",
        "[&>*:last-child:nth-child(odd)]:col-span-2 md:[&>*:last-child:nth-child(odd)]:col-span-1",
        COLUMNS[columns],
        className,
      )}
    >
      {children}
    </div>
  );
}
