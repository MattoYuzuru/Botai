import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

/** Одна деталь занятия: иконка по смыслу, текст и подпись для скринридера («Аудитория»). */
export interface ScheduleFact {
  key: string;
  icon: IconName;
  label: string;
  text: string;
}

/**
 * Детали занятия в карточке пары: иконка и текст в порядке, заданном пресетом `eventDetails`. Показывается
 * только то, что есть. `inline` — одной строкой с переносом (список дня), `stacked` — столбцом (узкая колонка
 * недели). Это не список: карточка целиком — кнопка, а вложенные списки в кнопке недопустимы.
 */
export function ScheduleFacts({
  facts,
  layout,
  className,
}: {
  facts: readonly ScheduleFact[] | undefined;
  layout: "inline" | "stacked";
  className?: string;
}) {
  if (!facts || facts.length === 0) return null;
  return (
    <span
      className={cn(
        "type-caption text-fg-muted",
        layout === "inline" ? "flex flex-wrap items-start gap-x-3 gap-y-1" : "flex flex-col gap-1",
        className,
      )}
    >
      {facts.map((fact) => (
        <span key={fact.key} data-fact={fact.key} className="inline-flex min-w-0 items-start gap-1">
          <Icon name={fact.icon} size={16} className="mt-0.5" />
          <span className="min-w-0 break-words">
            <span className="sr-only">{fact.label}: </span>
            {fact.text}
          </span>
        </span>
      ))}
    </span>
  );
}

/**
 * Бейдж типа занятия: слова источника бывают длинными («Текущий рубежный контроль»), поэтому он переносится на
 * вторую строку, а не усекается — информация не теряется в узкой колонке недели.
 */
export const KIND_BADGE = "h-auto min-h-6 max-w-full whitespace-normal py-0.5 text-left";

/**
 * Откуда склеена пара и в чём источники расходятся (FR-054). Строка источников — только когда их больше
 * одного («Кабинет · Календарь»), основной первым; строка расхождения — спокойное предупреждение рядом с
 * деталями, без рамки и заливки: цвет `warning-fg` вместе с иконкой и словами, а не один цвет. Как и
 * `ScheduleFacts`, это не список: карточка целиком — кнопка.
 */
export function ScheduleProvenance({
  sources,
  note,
  className,
}: {
  sources: readonly string[] | undefined;
  note: string | null | undefined;
  className?: string;
}) {
  const merged = sources !== undefined && sources.length > 1;
  if (!merged && !note) return null;
  return (
    <span className={cn("flex flex-col gap-1 type-caption", className)}>
      {merged && (
        <span data-part="sources" className="inline-flex min-w-0 items-start gap-1 text-fg-muted">
          <Icon name="source" size={16} className="mt-0.5" />
          <span className="min-w-0 break-words">
            <span className="sr-only">Источники: </span>
            {sources.join(" · ")}
          </span>
        </span>
      )}
      {note && (
        <span data-part="disagreement" className="inline-flex min-w-0 items-start gap-1 text-warning-fg">
          <Icon name="warning" size={16} className="mt-0.5" />
          <span className="min-w-0 break-words">{note}</span>
        </span>
      )}
    </span>
  );
}
