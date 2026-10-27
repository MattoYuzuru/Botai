import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface GradeTileProps {
  /** Оценка, как её дал источник: «8», «86», «зачтено». Ничего не пересчитывается. */
  value: string;
  /** Шкала: «10 баллов». Показывается рядом, если источник её назвал. */
  scale?: string | null;
  /** За что оценка: «Контрольная №2». */
  title?: string | null;
  /** Подпись: вес, дата, вуз. */
  meta?: ReactNode;
  onOpen?: () => void;
  className?: string;
}

/**
 * Плитка оценки (styleguide §4: плитки оценок — единственная сетка, две колонки от 430 px). Цифры табличные,
 * длинное значение переносится. Плитка не красит оценку «хорошей» или «плохой»: смысл шкалы знает только источник.
 */
export function GradeTile({ value, scale, title, meta, onOpen, className }: GradeTileProps) {
  const body = (
    <>
      <span className="flex flex-wrap items-baseline gap-x-2">
        <span className="break-words type-title text-fg" data-numeric="">
          {value}
        </span>
        {scale && <span className="type-caption text-fg-muted">{scale}</span>}
      </span>
      {title && <span className="type-subheading text-fg">{title}</span>}
      {meta && (
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">{meta}</span>
      )}
    </>
  );
  const shared =
    "flex min-h-20 w-full min-w-0 flex-col gap-1 rounded-lg border border-border bg-surface p-3 text-left";
  if (!onOpen) return <div className={cn(shared, className)}>{body}</div>;
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        shared,
        "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
        className,
      )}
    >
      {body}
    </button>
  );
}
